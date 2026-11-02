/**
 * QA1-FIX-06's log-body findings (part of #2596), drawn by Artifacts.tsx: a tool
 * row's gist (overview-agents-31), an empty stderr as a
 * sentence (-36, in qa1fix06.agentlogs), a result subtype in words (-37).
 * MUTATIONS at each assertion.
 */
import { describe, expect, it } from 'vitest'

import { inputGist, resultWords } from '../Artifacts'

describe('overview-agents-31: a tool row carries a one-line gist of its input', () => {
  it('names where it went or what it touched, else the first text field', () => {
    // MUTATION: return null from `inputGist` -- forty `tool · browser_click` rows read alike again.
    expect(inputGist('{\n  "element": "Sign in button",\n  "ref": "e12"\n}')).toBe('Sign in button')
    expect(inputGist('{"url": "https://example.com/"}')).toBe('https://example.com/')
    expect(inputGist('{"step": 3, "note": "a  note\\nover lines"}')).toBe('a note over lines')
    expect(inputGist('{"step": 3}')).toBeNull()
    expect(inputGist('not json')).toBeNull()
    expect(inputGist(null)).toBeNull()
    const long = inputGist(JSON.stringify({ command: 'x'.repeat(200) }))!
    // MUTATION: drop the cut -- a long command runs past the row.
    expect(long.length).toBe(80)
    expect(long.endsWith('…')).toBe(true)
  })
})

describe('overview-agents-37: a result row says how the run ended in words', () => {
  it('maps the CLI subtype, and keeps an unknown one by name', () => {
    // MUTATION: print `result · ${subtype}` for every subtype -- the raw enum is back.
    expect(resultWords('error_during_execution')).toBe('Stopped on an error')
    expect(resultWords('error_max_turns')).toBe('Stopped at its turn limit')
    expect(resultWords('success')).toBe('result · success')
    expect(resultWords('something_new')).toBe('result · something_new')
    expect(resultWords(undefined)).toBe('result')
  })
})
