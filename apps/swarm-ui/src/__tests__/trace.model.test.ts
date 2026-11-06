// THE TRACE'S MODEL (owner pick 2026-11-05, part of #2688): the transcript's
// steps as one row per event, read only from what the record holds.
// MUTATIONS are named at each assertion.

import { describe, expect, it } from 'vitest'

import { buildTrace, dur, inFilter, patchOf } from '../trace/model'
import { CWD, call, codingRecords, progress, reply, say, stepsOf } from './tracefixture'

describe('one row per recorded event', () => {
  const trace = buildTrace(stepsOf(codingRecords()))
  const labels = trace.events.map((e) => `${e.kind}:${e.label}`)

  it('folds a call, its progress and its result into one row, and keeps every other event', () => {
    // Mutation: a progress record drawn as its own row makes 13 rows.
    expect(trace.events).toHaveLength(11)
    expect(labels[0]).toBe('start:start')
    expect(labels[1]).toBe('message:Reading the contract before changing anything.')
    expect(labels[2]).toBe('tool:Read CONTRACT.md')
    const test = trace.events[4]!
    expect(test.progress).toBe(2)
    expect(test.result).not.toBeNull()
  })

  it('drops the working directory from commands and paths, and keeps it once', () => {
    expect(trace.cwd).toBe(CWD)
    // Mutation: no `shorten` leaves `cd /workspace/... && ` in the label.
    expect(trace.events[4]!.label).toBe('uv run pytest tests/unit/test_reconciler.py -q')
    expect(trace.events.some((e) => e.label.includes(CWD))).toBe(false)
  })

  it('reads a test run as a test, with pytest’s own counts', () => {
    expect(trace.events[4]!.kind).toBe('test')
    expect(trace.events[4]!.sub).toBe('1 failed · 11 passed')
    expect(trace.events[4]!.status).toBe('error')
    expect(trace.events[5]!.sub).toBe('12 passed')
    expect(trace.events[5]!.status).toBe('ok')
  })

  it('takes a duration only from what the record holds', () => {
    // The last progress report is a lower bound; a call without one has none.
    expect(trace.events[4]!.secs).toEqual({ value: 12, from: 'progress' })
    expect(trace.events[2]!.secs).toBeNull()
    const end = trace.events.at(-1)!
    expect(end.kind).toBe('end')
    expect(end.secs).toEqual({ value: 124, from: 'result' })
    expect(dur(12)).toBe('12s')
    expect(dur(124)).toBe('2m 4s')
  })

  it('draws an edit from its structured patch, with line numbers', () => {
    const edit = trace.events[3]!
    expect(edit.kind).toBe('edit')
    expect(edit.label).toBe('Edit reconciler.py')
    expect(edit.sub).toBe('+1 −1')
    expect(patchOf(edit)).toEqual([{ lines: [' a', '-b', '+c', ' d'], oldStart: 10, newStart: 10 }])
  })

  it('keeps a redacted thinking block and an unknown record as rows of their own', () => {
    expect(labels).toContain('thinking:thinking')
    expect(trace.events.find((e) => e.kind === 'thinking')!.sub).toBe('text not recorded')
    const other = trace.events.find((e) => e.kind === 'other')!
    expect(other.label).toBe('auth_status')
    expect(other.sub).toBe('as recorded')
  })

  it('counts the filters off the rows', () => {
    const count = (f: Parameters<typeof inFilter>[1]) => trace.events.filter((e) => inFilter(e, f)).length
    expect(count('all')).toBe(11)
    expect(count('messages')).toBe(2)
    expect(count('tools')).toBe(5)
    expect(count('edits')).toBe(2)
    expect(count('errors')).toBe(1)
  })
})

describe('what the record does not say is not made up', () => {
  it('a result whose call is not in this read is a row of its own, and progress with no call is counted', () => {
    const trace = buildTrace(stepsOf([progress('gone', 3), reply('gone', 'late output')]))
    expect(trace.events.map((e) => e.label)).toEqual(['1 progress event', 'tool result'])
    expect(trace.events[1]!.sub).toBe('its call is not in this read')
  })

  it('a call with no result yet is running, with no duration', () => {
    const trace = buildTrace(stepsOf([say('go'), call('t9', 'Bash', { command: 'make smoke' })]))
    expect(trace.events[1]!.status).toBe('running')
    expect(trace.events[1]!.secs).toBeNull()
  })

  it('names a browser step by its target, with or without the MCP prefix', () => {
    const trace = buildTrace(
      stepsOf([
        call('b1', 'mcp__playwright__browser_navigate', { url: 'https://example.com' }),
        call('b2', 'browser_click', { element: 'Learn more link', ref: 'e4' }),
        call('b3', 'mcp__playwright__browser_console_messages', {}),
        reply('b3', '[ERROR] boom\n[WARNING] slow\n[LOG] ok'),
      ]),
    )
    expect(trace.events.map((e) => `${e.kind}:${e.label}`)).toEqual([
      'browser:navigate https://example.com',
      'browser:click Learn more link',
      'browser:console_messages',
    ])
    expect(trace.events[2]!.sub).toBe('1 error · 1 warning')
  })
})
