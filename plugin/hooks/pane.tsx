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
// `/sc layout rows|pane` is answered here too, with no model turn: it saves
// the project's layout through `swarm_layout`. Where function hooks are off
// (they are early access: CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1) this module
// does not load, and commands/sc.md answers `/sc layout` the slower way.

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

    return next(e)
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

    return { text: 'Layout set to ' + data.layout + ' for this project. ' + (data.note || '') }
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
}

async function layoutCall($: EngineInterface, args: Record<string, unknown>) {
  try {
    return parseResult(await $.mcp.call(SERVER, LAYOUT, args))
  } catch (error) {
    return { ok: false as const, reason: error instanceof Error ? error.message : String(error) }
  }
}
