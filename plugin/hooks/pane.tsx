// The sc plugin's SwarmCloud pane (SC-BROWSER-BUILD, #2318): proposal B, Two
// columns, which BGD picked for every frame on 2026-10-25 (PR 2328).
//
// A Claude Code mod pane beside the chat: this session's workflows, then one
// workflow's stages on the left with the selected stage's agents -- then one
// agent's details -- on the right. `/sc-pane` opens it, and so does
// `/sc layout pane`. Everything it shows is read by ONE direct MCP call to the
// sc plugin's own SwarmCloud server per click (`$.mcp.call`, the engine's
// connection), so browsing never reaches the model and costs no model tokens.
// It reads when it opens, when something in it is opened, and on Refresh or
// Retry -- never on a timer, and nothing at all while it is closed.
//
// What it draws and what each click reads live in ./pane/model.ts, which a
// unit test runs under plain node; this module only answers the engine's
// events and turns the model's lines into elements.
//
// `/sc layout rows|pane` is answered here too: it saves the project's layout
// through `swarm_layout`, and then -- BGD, 2026-10-25: "Also re-attach
// automatically" -- the same run goes on as `/sc attach --all` (one chat
// turn), so rows in the new layout start beside the old ones, which keep
// running until they end. The engine refuses `$.command.run` inside a
// `command.run` hook, so the run is rewritten (`next` with the new args)
// rather than queued a second time. The pane's own Layout control is a
// press, outside any hook, so it queues the attach with `$.command.run`.
// Where function hooks are off (they are early access:
// CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1) this module does not load, and
// commands/sc.md answers `/sc layout` the slower way.
//
// AT SESSION START (BGD, 2026-10-25: "Yes, open it with no read"): ONE
// `swarm_layout` call per session learns the project's layout, after the
// session has started (the first `session.start` is awaited before the first
// prompt, and the bridge may still be starting). In the pane layout the pane
// opens drawing "not read yet" with Refresh, and reads nothing until Refresh.
// The engine seats an unasked pane only from 144 columns; below that it
// waits, and one toast says so. Either layout, Level 1 then lists this
// PROJECT's running work beside this session's, because session start
// tracks the project's running work (BGD's note; `/sc check` and the watch
// stay this session's).

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ScPaneState } from '../types'
import { LAYOUT, SERVER, act, frame, initialState, isNarrow, parseResult } from './pane/model.ts'

const PANE = 'swarmcloud'
const pane = atom({ plugin: 'sc', key: 'pane' } as const, initialState() as ScPaneState)

/** `/sc` as the engine names it: bare, or scoped by the plugin. */
const SC_COMMANDS = ['sc', 'sc:sc']

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'sc-pane',
      description: 'Open the SwarmCloud pane: this session’s workflows, their stages, agents and details, read on demand with no model tokens',
    })

    const started = await next(e)
    if (e.isInteractive && e.surface) void atStart($)
    return started
  })

  on('command.run', { command: 'sc-pane' }, async $ => {
    const opened = await $.ui.open({ id: PANE, title: 'SwarmCloud' })
    await run($, 'open')

    return {
      text: opened.isPlaced
        ? 'The SwarmCloud pane is open beside the chat. Browsing it reads SwarmCloud directly, so it spends no model tokens.'
        : 'The SwarmCloud pane is waiting for room: widen the terminal to see it.',
    }
  })

  on('command.run', async ($, e, next) => {
    const asked = /^\s*layout(?:\s+(\S+))?\s*$/.exec(e.args)
    if (SC_COMMANDS.indexOf(e.command) < 0 || !asked) {
      return next(e)
    }
    const wanted = (asked[1] || '').toLowerCase()
    if (wanted && wanted !== 'rows' && wanted !== 'pane') {
      return { text: '`' + asked[1] + '` is not a layout: `/sc layout rows` (today’s /workflows rows) or `/sc layout pane` (the SwarmCloud pane plus one row per workflow).' }
    }
    const reply = await layoutCall($, wanted ? { layout: wanted } : {})
    if (!reply.ok) {
      return { text: 'swarm_layout did not answer: ' + reply.reason }
    }
    const data = reply.data as { layout?: string; source?: string; note?: string }
    await update($, pane, state => ({ ...state, layout: data.layout === 'pane' || data.layout === 'rows' ? data.layout : state.layout }))
    if (!wanted) {
      return { text: 'Layout for this project: ' + data.layout + ' (' + data.source + '). `/sc layout rows` or `/sc layout pane` switches it.' }
    }
    if (wanted === 'pane') {
      await $.ui.open({ id: PANE, title: 'SwarmCloud' })
      await run($, 'open')
    }
    const said = 'Layout set to ' + data.layout + ' for this project. ' + SWITCHED
    try {
      // The re-attach: this same run goes on as `/sc attach --all`.
      const attach = await next({ ...e, args: 'attach --all' })
      return { ...attach, text: said + (attach.text ? '\n' + attach.text : '') }
    } catch (error) {
      return { text: said + ' It could not be queued (' + (error instanceof Error ? error.message : String(error)) + '): run `/sc attach --all` now.' }
    }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const state = await read($, pane)
    const cols = Math.max(20, e.props.bodyColumns)
    const lines = frame(state, { cols, narrow: isNarrow(e.viewport?.columns, cols), now: '' })

    return (
      <Box flexDirection="column">
        {lines.map((line, row) => (
          <Box key={'row' + row} flexDirection="row">
            {line.map((seg, col) => {
              const press = seg.press
              if (press !== undefined) {
                return (
                  <Button
                    key={press + '@' + row + '.' + col}
                    plain
                    label={seg.text}
                    hotkey={seg.hotkey}
                    onPress={() => {
                      void run($, press)
                    }}
                  />
                )
              }
              return (
                <Text key={'t' + row + '.' + col} color={seg.color} dimColor={seg.dim} bold={seg.bold}>
                  {seg.text}
                </Text>
              )
            })}
          </Box>
        ))}
      </Box>
    )
  })
}

/** One click: at most one MCP call (`act`), then the pane redraws from the new state. */
async function run($: EngineInterface, action: string): Promise<void> {
  if (action === 'console') {
    const state = await read($, pane)
    const link = state.agent && state.agent.console
    if (link) {
      await $.ui.copy({ text: link })
      $.ui.toast('Console link copied: ' + link)
    } else {
      $.ui.toast('SwarmCloud served no console link for this agent')
    }
    return
  }
  const before = await read($, pane)
  const started = await $.clock.now()
  const after = await act(
    before,
    action,
    async (tool, args) => parseResult(await $.mcp.call(SERVER, tool, args)),
    () => new Date(started).toISOString(),
  )
  await update($, pane, () => after)
  // The pane's Layout control saved a layout: re-attach, as `/sc layout` does.
  if (/^layout:(rows|pane)$/.test(action) && !after.failure) {
    $.command.run({ command: 'sc', args: 'attach --all' }).then(
      () => undefined,
      (error) => $.ui.toast('`/sc attach --all` could not be queued (' + (error instanceof Error ? error.message : String(error)) + '): run it now'),
    )
  }
}

/** What a switch says: the old rows keep going, and the attach starts the new ones. */
const SWITCHED = 'Rows already running in /workflows keep going until they end; `/sc attach --all` is queued now (one chat turn) and starts rows in the new layout beside them.'

/**
 * Session start: ONE swarm_layout call per session (a reload's session.start
 * asks nothing), and in the pane layout the pane opened unread. Never throws.
 */
async function atStart($: EngineInterface): Promise<void> {
  try {
    const before = await read($, pane)
    if (before.started) return
    await update($, pane, state => ({ ...state, started: true, project: true }))
    const reply = await layoutCall($, {})
    if (!reply.ok) return
    const data = reply.data as { layout?: string }
    const layout = data.layout === 'pane' || data.layout === 'rows' ? data.layout : null
    await update($, pane, state => ({ ...state, layout }))
    if (layout !== 'pane') return
    const opened = await $.ui.open({ id: PANE, title: 'SwarmCloud' })
    if (!opened.isPlaced) {
      $.ui.toast('The SwarmCloud pane is waiting: an unasked pane opens from 144 terminal columns. Widen the terminal, or run /sc-pane.', { timeoutMs: 10000 })
    }
  } catch {
    // Nothing at session start may stand in a session's way: /sc-pane still opens it.
  }
}

async function layoutCall($: EngineInterface, args: Record<string, unknown>) {
  try {
    return parseResult(await $.mcp.call(SERVER, LAYOUT, args))
  } catch (error) {
    return { ok: false as const, reason: error instanceof Error ? error.message : String(error) }
  }
}
