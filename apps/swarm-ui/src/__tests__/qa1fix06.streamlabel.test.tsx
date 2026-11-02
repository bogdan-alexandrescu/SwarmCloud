/**
 * QA1-FIX-06 overview-agents-38 (part of #2596): the stdout window's heading
 * was the raw stream name `agent_stdout`. MUTATION at the assertion.
 */
import { describe, expect, it } from 'vitest'

import { streamLabel } from '../RunFiles'

describe('overview-agents-38: the agent streams are named in words', () => {
  it('says `agent stdout`, never `agent_stdout`', () => {
    // MUTATION: drop the agent branch of `streamLabel` -- the stdout heading reads `agent_stdout`.
    expect(streamLabel('agent_stdout')).toBe('agent stdout')
    expect(streamLabel('agent_stderr')).toBe('agent stderr')
    expect(streamLabel('stdout')).toBe('runner stdout')
  })
})
