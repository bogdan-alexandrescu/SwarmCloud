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
//
// THE BAND (SC-BAND, part of #2318): BGD, 2026-10-26, moved the SwarmCloud
// view out of the right-docked pane into a HORIZONTAL STRIP ABOVE THE PROMPT,
// with one summary line BELOW the prompt -- a third layout, `band`, in which
// the plugin starts no /workflows rows at all. Why: in Claude Code's
// fullscreen layout a Pane docks on the right and a plugin cannot choose
// otherwise, and the /workflows list is Claude Code's own background-task
// list, where a plugin can only add model-driven rows. What a plugin can draw
// horizontally is the `AbovePrompt` band -- many rows, scrolled by the engine
// (`scroll.bodyRows`), folded by the engine's [-], held to half the terminal
// -- and `$.ui.status`, one pinned line under the prompt beside other
// plugins' lines.
//
// The band draws the pane's own levels (`frame`, from the same `$.state`
// value as the pane's browsing) under a summary line (`bandFrame`); its Fold
// leaves the summary alone, `SwarmCloud · 6 running · 1 waits on you · 2 PRs`,
// and while a survey holds the band it yields. Every read is one direct
// `swarm_browse` call, as the pane's are. It reads when it starts (session
// start in the band layout, with this project's work, or `/sc layout band`),
// on a press, and -- the one timer in this plugin, and only in this layout --
// once a minute WHILE ANYTHING LISTED RUNS; the minute stops when nothing
// runs, when the layout leaves `band`, and when the session ends. After each
// list read a workflow that finished, a step that waits for a person, a merge
// parked on owner questions, or new owner questions queues ONE chat turn,
// `/sc status <wf_id> · finished: succeeded · PR #2463` (`changes`); an
// unchanged read, and the first read, queue none. `/sc layout band` starts the
// band and queues no `/sc attach --all` (the layout attaches no rows); leaving
// `band` stops it and re-attaches as any other switch does. The band's code
// lives in this module because the engine follows `$` only into functions of
// the same file.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ScBandState, ScPaneState } from '../types'
import {
  BROWSE, LAYOUT, SERVER, act, anyRunning, bandFrame, changes, frame, initialBand, initialState, isNarrow, layoutOf,
  listArgs, parseResult, statusText, summaryText, takeList, unreachable,
} from './pane/model.ts'

const PANE = 'swarmcloud'
const pane = atom({ plugin: 'sc', key: 'pane' } as const, initialState() as ScPaneState)
const band = atom({ plugin: 'sc', key: 'band' } as const, initialBand() as ScBandState)

/** The band's re-read while anything runs: once a minute (BGD, 2026-10-26). */
const MINUTE_MS = 60_000

/** The minute's handle while it runs; nothing else is kept outside `$.state`. */
let minute: { cancel(): void } | null = null
let reading = false

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
    if (wanted && !layoutOf(wanted)) {
      return { text: '`' + asked[1] + '` is not a layout: `/sc layout rows` (today’s /workflows rows), `/sc layout pane` (the SwarmCloud pane plus one row per workflow) or `/sc layout band` (a strip above the prompt, no rows).' }
    }
    const reply = await layoutCall($, wanted ? { layout: wanted } : {})
    if (!reply.ok) {
      return { text: 'swarm_layout did not answer: ' + reply.reason }
    }
    const data = reply.data as { layout?: string; source?: string; note?: string }
    await update($, pane, state => ({ ...state, layout: layoutOf(data.layout) || state.layout }))
    if (!wanted) {
      return { text: 'Layout for this project: ' + data.layout + ' (' + data.source + '). `/sc layout rows`, `/sc layout pane` or `/sc layout band` switches it.' }
    }
    if (wanted === 'band') {
      // No re-attach: the band layout starts no rows. The band's one read is this session's and this project's work.
      await update($, pane, state => ({ ...state, project: true }))
      await startBand($)
      return { text: 'Layout set to band for this project. ' + BANDED }
    }
    await stopBand($)
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

  // THE BAND above the prompt, in the band layout.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const state = await read($, band)
    // Off, or a survey holds the band: the band is somebody else's.
    if (!state.on || e.props.hasSurvey) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const browse = await read($, pane)
    const cols = Math.max(20, e.props.bodyColumns)
    const lines = bandFrame(browse, state, { cols, rows: e.props.maxRows, narrow: isNarrow(e.viewport?.columns, cols) })

    return (
      <Box flexDirection="column">
        {lines.map((line, row) => (
          <Box key={'row' + row} flexDirection="row">
            {line.map((seg, col) => {
              const action = seg.press
              if (action !== undefined) {
                return (
                  <Button
                    key={action + '@' + row + '.' + col}
                    plain
                    label={seg.text}
                    hotkey={seg.hotkey}
                    onPress={() => {
                      void run($, action)
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

  // Never while the session is closed.
  on('session.end', async ($, e, next) => {
    stopMinute()
    return next(e)
  })
}

/**
 * One click, in the pane or the band: at most one MCP call (`act`), then both
 * redraw from the new state. The band's Fold and Open read nothing; a list
 * read goes on to the band's summary, status line and notices (`afterList`).
 */
async function run($: EngineInterface, action: string): Promise<void> {
  if (action === 'band:fold' || action === 'band:open') {
    await update($, band, state => ({ ...state, folded: action === 'band:fold' }))
    return
  }
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
  // A list read answered (a new list, even one read at the same second): the band's summary, line and notices.
  if (after.workflows !== before.workflows) await afterList($)
  // The Layout control saved the band: start it (no rows, so no attach).
  if (action === 'layout:band' && !after.failure) {
    await startBand($)
    return
  }
  // The Layout control saved a layout: the band stops, and re-attach, as `/sc layout` does.
  if (/^layout:(rows|pane)$/.test(action) && !after.failure) {
    await stopBand($)
    $.command.run({ command: 'sc', args: 'attach --all' }).then(
      () => undefined,
      (error) => $.ui.toast('`/sc attach --all` could not be queued (' + (error instanceof Error ? error.message : String(error)) + '): run it now'),
    )
  }
}

/** What a switch to the band says: the strip, the line under the prompt, and that no row starts. */
const BANDED = 'The SwarmCloud strip above the prompt lists this session’s and this project’s work, read directly with no model tokens, and the line under the prompt sums it up. No /workflows row starts in this layout; rows already running keep going until they end.'

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
    const layout = layoutOf(data.layout)
    await update($, pane, state => ({ ...state, layout }))
    // The band layout: the band opens with this session's and this project's running work.
    if (layout === 'band') {
      await startBand($)
      return
    }
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

/**
 * The band on: drawn above the prompt, and ONE list read -- this session's
 * work, and after session start this project's running work too (the pane
 * state's `project`). That read is the baseline: it queues no turn.
 */
async function startBand($: EngineInterface): Promise<void> {
  await update($, band, state => ({ ...state, on: true, seen: null }))
  await run($, 'open')
}

/** The band off: not drawn, no minute, and the line under the prompt cleared. */
async function stopBand($: EngineInterface): Promise<void> {
  stopMinute()
  const state = await read($, band)
  if (!state.on) return
  await update($, band, s => ({ ...s, on: false, seen: null, summary: null }))
  $.ui.status(undefined)
}

/**
 * After a list read: the summary, the line under the prompt, a chat turn for
 * each workflow whose read shows something new (`changes`), and the minute
 * kept running exactly while anything runs.
 */
async function afterList($: EngineInterface): Promise<void> {
  const state = await read($, band)
  if (!state.on) return
  const browse = await read($, pane)
  const { turns, seen } = changes(state.seen, browse)
  await update($, band, s => ({ ...s, seen, summary: summaryText(browse), turns: s.turns + turns.length }))
  $.ui.status(statusText(browse) ?? undefined)
  for (const args of turns) queue($, args)
  if (anyRunning(browse)) startMinute($)
  else stopMinute()
}

function startMinute($: EngineInterface): void {
  if (minute) return
  minute = $.clock.every(MINUTE_MS, () => {
    void tick($)
  })
}

function stopMinute(): void {
  if (minute) minute.cancel()
  minute = null
}

/** The minute: ONE swarm_browse list read, taken in without moving whoever browses. A failed read waits for the next minute. */
async function tick($: EngineInterface): Promise<void> {
  if (reading) return
  reading = true
  try {
    const state = await read($, band)
    if (!state.on) {
      stopMinute()
      return
    }
    const before = await read($, pane)
    let reply
    try {
      reply = parseResult(await $.mcp.call(SERVER, BROWSE, listArgs(before)))
    } catch (error) {
      reply = unreachable(error)
    }
    if (!reply.ok) return
    const data = reply.data
    const at = new Date(await $.clock.now()).toISOString()
    await update($, pane, s => takeList(s, data, at))
    await afterList($)
  } finally {
    reading = false
  }
}

/** One chat turn, `/sc <args>`, queued outside any hook; a refusal is said in a toast. */
function queue($: EngineInterface, args: string): void {
  $.command.run({ command: 'sc', args }).then(
    () => undefined,
    (error) => $.ui.toast('`/sc ' + args + '` could not be queued (' + (error instanceof Error ? error.message : String(error)) + '): run it now'),
  )
}
